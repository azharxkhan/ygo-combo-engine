"""Interruption check tests. Run with:  PYTHONPATH=src python3 -m pytest -q"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MECHABA, ASH, VEILER, DROLL = 75286621, 14558127, 97268402, 94145021
# Aleister the Invoker, Honest, Book of Moon, Effect Veiler, Twin Twisters
HAND = (86120751, 37742478, 14087893, 97268402, 43898403)


@pytest.fixture(scope="module")
def env():
    from ygocombo.engine import Environment, default_library_path

    if not default_library_path(ROOT).is_file() or not (ROOT / "vendor" / "CardScripts").is_dir():
        pytest.skip("engine not built (run scripts/fetch_vendor.sh and scripts/build_engine.sh)")
    return Environment.from_project_root(ROOT)


def _scenario(opponent):
    from ygocombo.deck import load_ydk
    from ygocombo.goldfish import Scenario

    return Scenario(load_ydk(ROOT / "decks" / "invoked_sample.ydk"), HAND, opponent_hand=opponent)


def _run(env, opponent):
    from ygocombo.interrupt import analyse
    from ygocombo.search import Limits

    return analyse(env, _scenario(opponent), (MECHABA,), lines=1,
                   limits=Limits(max_nodes=400, max_seconds=120),
                   recovery_limits=Limits(max_nodes=300, max_seconds=60))


def test_ash_on_the_only_search_stops_mechaba(env):
    result = _run(env, (ASH,))
    worst = result.reports[0].worst
    assert worst is not None and not worst.reached_goal
    assert "Ash Blossom" in worst.interruption.opponent[0]
    assert "Aleister" in worst.interruption.after_step


def test_veiler_is_played_through(env):
    report = _run(env, (VEILER,)).reports[0]
    assert len(report.outcomes) > 1  # Veiler can hit at several points
    assert all(o.reached_goal for o in report.outcomes)
    # The engine finds the answer: flip Aleister face-down with Book of Moon.
    on_aleister = [o for o in report.outcomes if o.interruption.after_steps == 2]
    assert any("Book of Moon" in o.steps[0] for o in on_aleister)


def test_two_hand_traps_are_tried_separately(env):
    report = _run(env, (ASH, DROLL)).reports[0]
    used = {o.interruption.opponent[0].split(" (")[0] for o in report.outcomes}
    assert used == {"Chain Ash Blossom & Joyous Spring", "Chain Droll & Lock Bird"}


def test_bad_settings_are_rejected(env):
    from ygocombo.goldfish import Scenario
    from ygocombo.interrupt import analyse

    with pytest.raises(ValueError, match="opponent"):
        analyse(env, _scenario(()), (MECHABA,))
    with pytest.raises(ValueError, match="lines"):
        analyse(env, _scenario((ASH,)), (MECHABA,), lines=0)
    with pytest.raises(ValueError, match="opponent hand"):
        Scenario(_scenario(()).deck, HAND, opponent_hand=(ASH,) * 7)
