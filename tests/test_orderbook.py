from kbot.kalshi.orderbook import OrderBook
from kbot.kalshi.prices import (
    cents_to_dc,
    count_to_fp,
    dc_to_cents,
    dc_to_dollars,
    dollars_to_dc,
    format_cents,
    format_dollars,
    parse_count,
    parse_levels,
)


def make_book() -> OrderBook:
    """A book in the wire format the API actually emits."""
    book = OrderBook("TEST")
    book.apply_snapshot(
        yes=[["0.4000", "100.00"], ["0.3900", "50.00"], ["0.3800", "25.00"]],
        no=[["0.5500", "40.00"], ["0.5400", "20.00"], ["0.5300", "10.00"]],
    )
    return book


# ---------------- units ----------------


def test_dollars_round_trip_through_deci_cents():
    assert dollars_to_dc("0.5600") == 560
    assert dollars_to_dc("0.0010") == 1  # deci-cent tick in the tail
    assert dollars_to_dc("1.0000") == 1000
    assert dc_to_dollars(560) == "0.5600"
    assert dc_to_dollars(1) == "0.0010"


def test_deci_cent_ticks_survive_conversion():
    # Kalshi ticks in tenths of a cent below 10c; whole cents would lose these.
    for wire in ("0.0010", "0.0230", "0.9910"):
        assert dc_to_dollars(dollars_to_dc(wire)) == wire


def test_cents_helpers():
    assert cents_to_dc(65) == 650
    assert dc_to_cents(650) == 65
    assert format_cents(650) == "65c"
    assert format_cents(23) == "2.3c"  # tail price keeps its tenth
    assert format_dollars(1234) == "1.23"


def test_count_parsing():
    assert parse_count("10.00") == 10.0
    assert parse_count("2.50") == 2.5
    assert count_to_fp(3) == "3.00"


def test_parse_levels_skips_junk_and_zero_size():
    levels = parse_levels(
        [["0.4000", "100.00"], ["bad", "1.00"], ["0.3000", "0.00"], ["0.2000", "5.00"]]
    )
    assert levels == [(400, 100.0), (200, 5.0)]


def test_parse_levels_handles_empty():
    assert parse_levels(None) == []
    assert parse_levels([]) == []


# ---------------- book ----------------


def test_best_prices_mirror_across_sides():
    book = make_book()
    assert book.best_yes_bid == 400
    assert book.best_no_bid == 550
    # A YES ask is the mirror of the best NO bid.
    assert book.yes_ask == 450
    assert book.no_ask == 600
    assert book.spread == 50  # 5 cents
    assert book.mid == 425


def test_delta_adds_and_removes_levels():
    book = make_book()
    book.apply_delta("yes", 410, 30)
    assert book.best_yes_bid == 410
    book.apply_delta("yes", 410, -30)
    assert book.best_yes_bid == 400
    assert 410 not in book.yes


def test_delta_clears_level_when_it_goes_non_positive():
    book = make_book()
    book.apply_delta("no", 550, -40)
    assert 550 not in book.no
    assert book.best_no_bid == 540


def test_fractional_residue_clears_the_level():
    book = make_book()
    # Fractional contracts can leave a rounding crumb; below the 0.01 minimum
    # the level is gone, not "almost gone".
    book.apply_delta("yes", 400, -99.999)
    assert 400 not in book.yes


def test_imbalance_is_signed_toward_the_heavier_side():
    book = make_book()
    imbalance = book.imbalance()
    assert imbalance is not None and imbalance > 0
    assert round(imbalance, 4) == round((175 - 70) / 245, 4)


def test_imbalance_is_none_on_an_empty_book():
    assert OrderBook("EMPTY").imbalance() is None


def test_microprice_leans_toward_the_heavier_side():
    book = make_book()
    micro = book.microprice()
    assert micro is not None
    assert micro > book.mid


def test_size_at_ask_reads_the_mirrored_ladder():
    book = make_book()
    # Buying YES lifts the best NO bid, where 40 contracts rest.
    assert book.size_at_ask("yes") == 40.0
    assert book.size_at_ask("no") == 100.0


def test_best_ask_by_side():
    book = make_book()
    assert book.best_ask("yes") == 450
    assert book.best_ask("no") == 600
    assert book.best_bid("yes") == 400
    assert book.best_bid("no") == 550


def test_stale_until_first_update():
    book = OrderBook("TEST")
    assert book.is_stale
    book.apply_snapshot([["0.1000", "1.00"]], [["0.1000", "1.00"]])
    assert not book.is_stale
    assert book.age() < 1
