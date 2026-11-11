"""Deck importer tests. Run with:  PYTHONPATH=src python3 -m pytest -q

The ydke tests need no data. The rest need the card database
(scripts/fetch_vendor.sh) and are skipped without it.
"""

import base64
import struct
from pathlib import Path

import pytest

from ygocombo.deck import Deck, DeckError, load_ydk
from ygocombo.importer import (
    check_deck_name, parse_text, parse_text_list, parse_ydke, save_deck, to_ydke,
)

ROOT = Path(__file__).resolve().parents[1]
ASH, VEILER, NIBIRU, MECHABA, ALEISTER = 14558127, 97268402, 27204311, 75286621, 86120751


def test_ydke_matches_spec_example():
    # From the YDKe spec: bytes A3 A9 57 05 (little endian) = passcode 89631139.
    assert parse_ydke("ydke://o6lXBQ==!!!") == Deck(main=[89631139])


def test_ydke_round_trip():
    deck = load_ydk(ROOT / "decks" / "invoked_sample.ydk")
    assert parse_ydke(to_ydke(deck)) == deck


def test_ydke_without_padding_or_last_bang():
    assert parse_ydke("ydke://o6lXBQ!!") == Deck(main=[89631139])


@pytest.mark.parametrize("bad", [
    "ydke://o6lXBQ==",                    # missing sections
    "ydke://o6lXBQ==!!!!",                # too many sections
    "ydke://o6lX!!!",                     # 3 bytes, not a whole card code
    "ydke://o6l*BQ==!!!",                 # not base64
    "ydke://AAAAAA==!!!",                 # card code 0
    "ydke://" + base64.b64encode(b"\x01\x00\x00\x00" * 101).decode() + "!!!",  # absurd size
])
def test_ydke_rejects_garbage(bad):
    with pytest.raises(DeckError):
        parse_ydke(bad)


@pytest.mark.parametrize("name", ["my_deck", "Invoked-2026", "a"])
def test_deck_names_ok(name):
    assert check_deck_name(name) == name


@pytest.mark.parametrize("name", ["", "../evil", "a/b", "a.ydk", "-x", "x" * 65, "con fig", "C:\\x", "CON", "nul", "Com1"])
def test_deck_names_rejected(name):
    with pytest.raises(DeckError):
        check_deck_name(name)


@pytest.fixture(scope="module")
def db():
    from ygocombo.carddb import CardDatabase
    from ygocombo.engine import project_databases

    paths = project_databases(ROOT)
    if not paths[0].is_file():
        pytest.skip("card database missing (run scripts/fetch_vendor.sh)")
    return CardDatabase(paths)


def test_text_list_formats(db):
    text = """
    // a comment
    Main Deck (5):
    3 Ash Blossom & Joyous Spring
    1x Effect Veiler
    Nibiru, the Primal Being x1
    Extra Deck
    Invoked Mechaba
    Side:
    97268402
    """
    deck = parse_text_list(text, db)
    assert deck == Deck(main=[ASH, ASH, ASH, VEILER, NIBIRU], extra=[MECHABA], side=[VEILER])


def test_text_list_without_headers_sorts_extra_deck(db):
    deck = parse_text_list("2 Aleister the Invoker\nInvoked Mechaba", db)
    assert deck == Deck(main=[ALEISTER, ALEISTER], extra=[MECHABA])


def test_names_starting_with_a_number(db):
    fish = db.find_by_name("7 Colored Fish").code
    assert parse_text_list("7 Colored Fish\n2 7 Colored Fish", db).main == [fish] * 3


@pytest.mark.parametrize("bad, message", [
    ("3 Ash Blossom", "unknown card"),             # partial name, never guessed
    ("4 Ash Blossom & Joyous Spring", "copy count"),
    ("0 Ash Blossom & Joyous Spring", "copy count"),
    ("Ash Blossom & Joyous Spring x9", "copy count"),
    ("Main Deck\n", "no cards"),
    ("x" * 300, "too long"),
])
def test_text_list_rejects(db, bad, message):
    with pytest.raises(DeckError, match=message):
        parse_text_list(bad, db)


def test_parse_text_detects_format(db):
    assert parse_text("ydke://o6lXBQ==!!!", db)[1] == "ydke"
    assert parse_text("#main\n14558127\n#extra\n!side\n", db)[1] == "ydk"
    assert parse_text("Ash Blossom & Joyous Spring", db)[1] == "text list"


def test_save_deck(tmp_path):
    deck = Deck(main=[ASH], extra=[MECHABA])
    path = save_deck(deck, tmp_path, "test")
    assert path == tmp_path / "test.ydk"
    assert load_ydk(path) == deck
    with pytest.raises(DeckError, match="already exists"):
        save_deck(deck, tmp_path, "test")
    save_deck(Deck(main=[VEILER]), tmp_path, "test", overwrite=True)
    assert load_ydk(path).main == [VEILER]
