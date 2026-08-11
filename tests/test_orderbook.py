from kbot.kalshi.orderbook import OrderBook


def make_book() -> OrderBook:
    book = OrderBook("TEST")
    book.apply_snapshot(
        yes=[[40, 100], [39, 50], [38, 25]],
        no=[[55, 40], [54, 20], [53, 10]],
    )
    return book


def test_best_prices_mirror_across_sides():
    book = make_book()
    assert book.best_yes_bid == 40
    assert book.best_no_bid == 55
    # A YES ask is the mirror of the best NO bid.
    assert book.yes_ask == 45
    assert book.no_ask == 60
    assert book.spread == 5
    assert book.mid == 42.5


def test_delta_adds_and_removes_levels():
    book = make_book()
    book.apply_delta("yes", 41, 30)
    assert book.best_yes_bid == 41
    book.apply_delta("yes", 41, -30)
    assert book.best_yes_bid == 40
    assert 41 not in book.yes


def test_delta_clears_level_when_it_goes_non_positive():
    book = make_book()
    book.apply_delta("no", 55, -40)
    assert 55 not in book.no
    assert book.best_no_bid == 54


def test_imbalance_is_signed_toward_the_heavier_side():
    book = make_book()
    imbalance = book.imbalance()
    # 175 YES vs 70 NO contracts resting.
    assert imbalance is not None and imbalance > 0
    assert round(imbalance, 4) == round((175 - 70) / 245, 4)


def test_imbalance_is_none_on_an_empty_book():
    assert OrderBook("EMPTY").imbalance() is None


def test_microprice_leans_toward_the_heavier_side():
    book = make_book()
    micro = book.microprice()
    assert micro is not None
    # YES depth dominates, so fair value sits above the simple mid.
    assert micro > book.mid


def test_best_ask_by_side():
    book = make_book()
    assert book.best_ask("yes") == 45
    assert book.best_ask("no") == 60
    assert book.best_bid("yes") == 40
    assert book.best_bid("no") == 55


def test_stale_until_first_update():
    book = OrderBook("TEST")
    assert book.is_stale
    book.apply_snapshot([[10, 1]], [[10, 1]])
    assert not book.is_stale
    assert book.age() < 1
