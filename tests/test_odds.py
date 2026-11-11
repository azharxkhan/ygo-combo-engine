"""Hand odds tests. Run with:  PYTHONPATH=src python3 -m pytest -q

The maths and sampling tests need no data. The search tests need the built
engine and are skipped without it.
"""

from collections import Counter
from pathlib import Path

import pytest

from ygocombo.deck import load_ydk
from ygocombo.odds import HANDS_MAX, OddsResult, HandResult, sample_hands, wilson_interval

ROOT = Path(__file__).resolve().parents[1]
MECHABA = 75286621
DECK = load_ydk(ROOT / "decks" / "invoked_sample.ydk")


def test_wilson_interval_known_values():
    low, high = wilson_interval(7, 10)
    assert round(low, 3) == 0.397 and round(high, 3) == 0.892
    assert wilson_interval(0, 10)[0] == 0.0
    assert wilson_interval(10, 10)[1] == pytest.approx(1.0)
    with pytest.raises(ValueError):
        wilson_interval(11, 10)


def test_sample_hands_are_repeatable_and_legal():
    first = list(sample_hands(DECK, 20, 5, seed=7))
    assert first == list(sample_hands(DECK, 20, 5, seed=7))
    assert first != list(sample_hands(DECK, 20, 5, seed=8))
    deck_counts = Counter(DECK.main)
    for hand, _ in first:
        assert len(hand) == 5
        assert not Counter(hand) - deck_counts  # never more copies than the deck has


@pytest.mark.parametrize("count, size, seed", [
    (0, 5, 1), (HANDS_MAX + 1, 5, 1), (10, 4, 1), (10, 7, 1), (10, 5, -1), (10, 5, 2**32),
])
def test_sample_hands_rejects_bad_input(count, size, seed):
    with pytest.raises(ValueError):
        next(sample_hands(DECK, count, size, seed))


def test_rate_range_counts_unknown_both_ways():
    result = OddsResult(hands=[HandResult((1,), o, 0.0, 0) for o in ("hit", "miss", "unknown", "hit")])
    assert result.low == 0.5 and result.high == 0.75


@pytest.fixture(scope="module")
def env():
    from ygocombo.engine import Environment, default_library_path

    if not default_library_path(ROOT).is_file() or not (ROOT / "vendor" / "CardScripts").is_dir():
        pytest.skip("engine not built (run scripts/fetch_vendor.sh and scripts/build_engine.sh)")
    return Environment.from_project_root(ROOT)


def test_search_stops_at_goal(env):
    from ygocombo.goldfish import Scenario
    from ygocombo.search import BoardScorer, ComboSearch, Limits

    # Aleister the Invoker, Honest, Book of Moon, Effect Veiler, Twin Twisters
    hand = (86120751, 37742478, 14087893, 97268402, 43898403)
    result = ComboSearch(
        env, Scenario(DECK, hand), limits=Limits(max_nodes=2000, max_seconds=120),
        scorer=BoardScorer(env, (MECHABA,)), keep=1, stop_at_goal=True,
    ).run()
    assert result.reached_goal and result.stopped_by == "goal"
    assert result.goal_steps and result.nodes < 200
    assert any("Invoked Mechaba" in step for step in result.goal_steps)


def test_run_odds_small_sample(env):
    from ygocombo.odds import run_odds
    from ygocombo.search import Limits

    seen = []
    result = run_odds(env, DECK, (MECHABA,), hands=4, seed=1,
                      limits=Limits(max_nodes=300, max_seconds=20),
                      on_hand=lambda number, hand: seen.append(number))
    assert seen == [1, 2, 3, 4] and len(result.hands) == 4
    # Seed 1 starts with Aleister + Effect Veiler (a LIGHT monster): a sure hit.
    assert result.hands[0].outcome == "hit"
    with pytest.raises(ValueError):
        run_odds(env, DECK, (), hands=1)
