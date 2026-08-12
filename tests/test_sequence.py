"""Sequence validation on the live order book.

A dropped delta frame is the most dangerous failure this system can have,
because it does not look like a failure. The book still has levels, the levels
are still plausible, the spread is still tight -- it is simply no longer the
exchange's book, and every order priced off it is priced off fiction.

`seq` was recorded before this and never checked.
"""

from __future__ import annotations

from kbot.kalshi.orderbook import OrderBook


def book_with_snapshot(seq: int = 1) -> OrderBook:
    book = OrderBook("KXBTC15M-TEST")
    # Wire ladders are fixed-point dollar strings.
    book.apply_snapshot([["0.4500", "100"]], [["0.5300", "100"]], seq)
    return book


def test_a_snapshot_establishes_the_sequence():
    book = book_with_snapshot(7)
    assert book.seq == 7
    assert not book.desynced
    assert not book.is_stale


def test_deltas_in_order_are_applied():
    book = book_with_snapshot(1)
    assert book.apply_delta("yes", 450, 5.0, 2) is True
    assert book.apply_delta("yes", 450, 5.0, 3) is True
    assert book.seq == 3
    assert not book.desynced
    assert book.yes[450] == 110.0


def test_a_gap_marks_the_book_desynced_and_drops_the_delta():
    """Applying the delta anyway is the tempting choice and the wrong one: it
    produces a book that looks right and is not."""
    book = book_with_snapshot(1)
    before = dict(book.yes)

    assert book.apply_delta("yes", 450, 50.0, 5) is False  # 2,3,4 missing

    assert book.desynced
    assert book.yes == before, "a delta after a gap must not be applied"


def test_a_desynced_book_reports_itself_stale():
    """This is what stops it pricing an order. Nothing else about the book
    looks wrong, so the flag has to be what the trading path consults."""
    book = book_with_snapshot(1)
    assert not book.is_stale
    book.apply_delta("yes", 450, 1.0, 9)
    assert book.is_stale


def test_a_duplicate_frame_is_ignored_but_is_not_a_desync():
    """Replays and retransmits lose nothing, so recovering from them would be
    an expensive answer to a non-problem."""
    book = book_with_snapshot(5)
    book.apply_delta("yes", 450, 10.0, 6)
    qty = book.yes[450]

    assert book.apply_delta("yes", 450, 10.0, 6) is True  # same frame again
    assert book.apply_delta("yes", 450, 10.0, 4) is True  # older frame
    assert book.yes[450] == qty, "a duplicate must not be applied twice"
    assert not book.desynced


def test_a_fresh_snapshot_clears_the_desync():
    """Ground truth, so it is the one thing that can restore the book."""
    book = book_with_snapshot(1)
    book.apply_delta("yes", 450, 1.0, 99)
    assert book.desynced

    book.apply_snapshot([["0.4600", "80"]], [["0.5200", "80"]], 100)
    assert not book.desynced
    assert not book.is_stale
    assert book.seq == 100


def test_deltas_resume_normally_after_recovery():
    book = book_with_snapshot(1)
    book.apply_delta("yes", 450, 1.0, 50)
    book.apply_snapshot([["0.4600", "80"]], [["0.5200", "80"]], 100)
    assert book.apply_delta("yes", 460, 5.0, 101) is True
    assert book.yes[460] == 85.0


def test_a_feed_without_sequence_numbers_still_works():
    """Some frames arrive without a seq. Treating a missing number as a gap
    would desync a book that is perfectly fine."""
    book = book_with_snapshot(1)
    assert book.apply_delta("yes", 450, 5.0, None) is True
    assert not book.desynced
