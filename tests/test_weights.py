from app.weights import Slot, rebalance, split_even


def test_split_even_sums_to_100():
    assert split_even(100, 3) == [34, 33, 33]
    assert split_even(100, 4) == [25, 25, 25, 25]
    assert split_even(75, 2) == [38, 37]
    assert split_even(100, 0) == []
    assert split_even(-20, 2) == [0, 0]


def test_removed_get_zero():
    slots = [Slot(50, True), Slot(50, False), Slot(0, True)]
    assert rebalance(slots) == [50, 0, 50]


def test_pinned_keeps_share():
    # пример из видео: закреплён 25%, остальные двое делят 75%
    assert rebalance([Slot(25, True, True), Slot(10, True), Slot(10, True)]) == [25, 38, 37]


def test_only_pinned_left_is_allowed_in_draft():
    # черновик может временно не давать 100% — это ловит публикация, а не пересчёт
    assert rebalance([Slot(30, True, True), Slot(70, False)]) == [30, 0]


def test_pinned_removed_is_ignored():
    assert rebalance([Slot(80, False, True), Slot(0, True)]) == [0, 100]


def test_frozen_keeps_share_and_is_not_counted():
    # выключенный в Keitaro оффер сохраняет долю и не влияет на пересчёт активных
    assert rebalance([Slot(40, False, frozen=True), Slot(0, True), Slot(0, True)]) == [40, 50, 50]
