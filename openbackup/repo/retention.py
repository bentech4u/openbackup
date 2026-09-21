"""Choosing which restore points to keep.

Deleting a point here only removes its metadata and block maps. The chunks it
referenced stay until garbage collection runs, because other points very
likely share them -- that separation is what makes retention cheap and safe to
interrupt.

A grandfather-father-son policy keeps the most recent points plus one per
day, week, month and year. Points are selected by the period they fall in and
the *newest* point in each period wins, which is the convention operators
expect: "the daily for Tuesday" means the last backup taken on Tuesday.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from .restorepoint import RestorePoint


@dataclass
class RetentionPolicy:
    """How many points to keep at each granularity.

    None means "no limit at this granularity"; 0 means "keep none".
    """

    keep_last: int | None = 7
    keep_daily: int | None = None
    keep_weekly: int | None = None
    keep_monthly: int | None = None
    keep_yearly: int | None = None

    @property
    def keeps_anything(self) -> bool:
        return any(v not in (None, 0) for v in (
            self.keep_last, self.keep_daily, self.keep_weekly,
            self.keep_monthly, self.keep_yearly))


def _parsed(point: RestorePoint) -> datetime:
    return datetime.fromisoformat(point.created_at)


_PERIODS = {
    "keep_daily": lambda d: (d.year, d.month, d.day),
    "keep_weekly": lambda d: d.isocalendar()[:2],
    "keep_monthly": lambda d: (d.year, d.month),
    "keep_yearly": lambda d: (d.year,),
}


def select(points: Iterable[RestorePoint], policy: RetentionPolicy
           ) -> tuple[list[RestorePoint], list[RestorePoint]]:
    """Split points into (keep, expire), newest first within each list."""
    ordered = sorted(points, key=_parsed, reverse=True)
    if not ordered:
        return [], []

    # A policy that keeps nothing would delete every backup on the first run.
    # That is never what someone meant to configure, so refuse it.
    if not policy.keeps_anything:
        raise ValueError(
            "retention policy would keep no restore points at all; set at "
            "least one of keep_last, keep_daily, keep_weekly, keep_monthly "
            "or keep_yearly"
        )

    keep: set[str] = set()

    if policy.keep_last:
        keep.update(p.id for p in ordered[:policy.keep_last])

    for attr, period_of in _PERIODS.items():
        limit = getattr(policy, attr)
        if not limit:
            continue
        seen: set = set()
        for point in ordered:          # newest first: first seen per period wins
            period = period_of(_parsed(point))
            if period in seen:
                continue
            seen.add(period)
            keep.add(point.id)
            if len(seen) >= limit:
                break

    kept = [p for p in ordered if p.id in keep]
    expired = [p for p in ordered if p.id not in keep]
    return kept, expired


def apply(store, vm_uuid: str, policy: RetentionPolicy, *,
          dry_run: bool = False) -> tuple[list[RestorePoint], list[RestorePoint]]:
    """Apply a policy to one VM's points, deleting the expired ones."""
    points = [store.load(vm_uuid, pid) for pid in store.list_points(vm_uuid)]
    kept, expired = select(points, policy)
    if not dry_run:
        for point in expired:
            store.delete(vm_uuid, point.id)
    return kept, expired
