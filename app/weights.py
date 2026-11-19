"""Пересчёт долей офферов в потоке.

Правило из видео: закреплённые (pinned) активные офферы сохраняют долю,
остаток до 100% делится поровну между незакреплёнными активными.
Удалённые офферы получают 0%. Доли целые (Keitaro принимает целые проценты),
остаток от деления отдаётся первым по порядку офферам: 3 оффера -> 34/33/33.

Функция никогда не падает: в черновике сумма может временно быть не 100%
(например, остались только закреплённые офферы). Сумму проверяет публикация.
Офферы, которые не участвуют в пересчёте (выключенные в Keitaro), передаются
как frozen и сохраняют долю.
"""
from dataclasses import dataclass


@dataclass
class Slot:
    share: int
    active: bool
    pinned: bool = False
    frozen: bool = False


def split_even(total: int, n: int) -> list[int]:
    if n <= 0:
        return []
    base, rest = divmod(max(total, 0), n)
    return [base + (1 if i < rest else 0) for i in range(n)]


def pinned_total(slots: list[Slot]) -> int:
    return sum(s.share for s in slots if s.active and s.pinned and not s.frozen)


def rebalance(slots: list[Slot]) -> list[int]:
    """Возвращает новые доли в том же порядке, что и slots."""
    free_idx = [i for i, s in enumerate(slots) if s.active and not s.pinned and not s.frozen]
    shares = iter(split_even(100 - pinned_total(slots), len(free_idx)))

    result: list[int] = []
    for s in slots:
        if s.frozen:
            result.append(s.share)
        elif not s.active:
            result.append(0)
        elif s.pinned:
            result.append(s.share)
        else:
            result.append(next(shares))
    return result
