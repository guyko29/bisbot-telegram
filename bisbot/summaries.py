"""Daily and weekly numbers, computed in Python from DB totals.

Nothing here talks to Claude or Telegram: functions take plain totals
({category: portions}) and return dataclasses, Hebrew text, or JSON-ready
dicts that are later handed to Claude purely for phrasing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from .menu import Category, Menu
from .texts import fmt_day, fmt_num, fmt_qty

EPSILON = 0.01
# A category counts as "consistently" off when it misses the same way on at
# least this share of the logged days (and there are enough days to judge).
CONSISTENT_SHARE = 0.6
MIN_DAYS_FOR_PATTERN = 3


def on_target_tolerance(target: float) -> float:
    """How far from the target still counts as hitting it: half a portion, or 10%."""
    return max(0.5, 0.1 * target)


# ---- one day -----------------------------------------------------------


@dataclass(frozen=True)
class CategoryStatus:
    category: Category
    eaten: float

    @property
    def target(self) -> float | None:
        return self.category.daily_target

    @property
    def remaining(self) -> float | None:
        """Portions still available today; None when the category has no target."""
        if self.target is None:
            return None
        return round(max(0.0, self.target - self.eaten), 2)

    @property
    def over(self) -> float:
        """Portions eaten beyond the target (0 when within it or no target)."""
        if self.target is None:
            return 0.0
        return round(max(0.0, self.eaten - self.target), 2)


def day_status(totals: dict[str, float], menu: Menu) -> list[CategoryStatus]:
    """Eaten vs. target for every menu category, in menu order."""
    return [
        CategoryStatus(category=category, eaten=round(totals.get(category.key, 0.0), 2))
        for category in menu.categories
    ]


def format_remaining(statuses: list[CategoryStatus]) -> str:
    """One line listing what is still left today."""
    left = [s for s in statuses if s.remaining is not None and s.remaining > EPSILON]
    if not left:
        if any(s.target is not None for s in statuses):
            return "🎯 הגעת לכל היעדים להיום!"
        return ""
    parts = [f"{s.category.name} {fmt_num(s.remaining)}" for s in left]
    return "נשאר להיום: " + " · ".join(parts)


def format_over(statuses: list[CategoryStatus]) -> str:
    """One line listing categories that went past today's target."""
    over = [s for s in statuses if s.over > EPSILON]
    if not over:
        return ""
    parts = [f"{s.category.name} {fmt_num(s.over)}" for s in over]
    return "⚠️ מעבר ליעד: " + " · ".join(parts)


def format_balance(statuses: list[CategoryStatus]) -> str:
    """What is left today and what went over, as one or two short lines."""
    return "\n".join(line for line in (format_remaining(statuses), format_over(statuses)) if line)


def format_day(statuses: list[CategoryStatus], day: date, *, title: str = "📊 סיכום") -> str:
    lines = [f"{title} · {fmt_day(day)}", ""]
    for status in statuses:
        category = status.category
        if status.target is None:
            lines.append(f"{category.name}: {fmt_qty(status.eaten, category)} (ללא יעד יומי)")
            continue
        line = f"{category.name}: {fmt_num(status.eaten)} מתוך {fmt_qty(status.target, category)}"
        if status.over > EPSILON:
            line += f" · חריגה של {fmt_num(status.over)} ⚠️"
        elif status.remaining is not None and status.remaining > EPSILON:
            line += f" · נשארו {fmt_num(status.remaining)}"
        else:
            line += " ✅"
        lines.append(line)
    remaining = format_remaining(statuses)
    if remaining:
        lines += ["", remaining]
    return "\n".join(lines)


def day_payload(statuses: list[CategoryStatus], day: date) -> dict[str, Any]:
    """The day's numbers in a JSON-ready shape, for Claude to phrase."""
    return {
        "date": day.isoformat(),
        "weekday": day.strftime("%A"),
        "categories": [
            {
                "key": s.category.key,
                "name": s.category.name,
                "eaten": s.eaten,
                "daily_target": s.target,
                "remaining": s.remaining,
                "over": s.over,
            }
            for s in statuses
        ],
    }


# ---- seven days --------------------------------------------------------


@dataclass(frozen=True)
class WeekCategoryStats:
    category: Category
    total: float
    avg_per_logged_day: float
    adherence_pct: float | None  # None when the category has no target
    days_on_target: int = 0
    days_under: int = 0
    days_over: int = 0


@dataclass(frozen=True)
class WeekStats:
    days: list[date]
    logged_days: list[date]
    categories: list[WeekCategoryStats]
    per_day: dict[str, dict[str, float]] = field(default_factory=dict)
    patterns: list[str] = field(default_factory=list)  # Hebrew, human readable

    @property
    def unlogged_days(self) -> list[date]:
        return [d for d in self.days if d not in self.logged_days]

    @property
    def overall_adherence_pct(self) -> float | None:
        scored = [c.adherence_pct for c in self.categories if c.adherence_pct is not None]
        return round(sum(scored) / len(scored), 1) if scored else None


def week_days(last_day: date, length: int = 7) -> list[date]:
    """The `length` consecutive days ending on last_day, oldest first."""
    return [last_day - timedelta(days=offset) for offset in range(length - 1, -1, -1)]


def _adherence_score(eaten: float, target: float) -> float:
    """1.0 exactly on target, falling linearly to 0 at 100% off (either way)."""
    if target <= 0:
        return 1.0 if eaten <= EPSILON else 0.0
    return max(0.0, 1.0 - abs(eaten - target) / target)


def week_stats(
    totals_by_day: dict[str, dict[str, float]], menu: Menu, days: list[date]
) -> WeekStats:
    """Per-category adherence over `days`.

    Days without any logged meal are left out of averages and adherence: no
    log means "unknown", not "ate nothing". They are reported separately.
    """
    logged = [d for d in days if totals_by_day.get(d.isoformat())]
    categories: list[WeekCategoryStats] = []
    for category in menu.categories:
        eaten_per_day = [
            totals_by_day[d.isoformat()].get(category.key, 0.0) for d in logged
        ]
        total = round(sum(eaten_per_day), 2)
        avg = round(total / len(logged), 2) if logged else 0.0
        target = category.daily_target
        if target is None or not logged:
            categories.append(WeekCategoryStats(category, total, avg, None))
            continue
        tolerance = on_target_tolerance(target)
        under = sum(1 for eaten in eaten_per_day if eaten < target - tolerance - EPSILON)
        over = sum(1 for eaten in eaten_per_day if eaten > target + tolerance + EPSILON)
        scores = [_adherence_score(eaten, target) for eaten in eaten_per_day]
        categories.append(
            WeekCategoryStats(
                category=category,
                total=total,
                avg_per_logged_day=avg,
                adherence_pct=round(100 * sum(scores) / len(scores), 1),
                days_on_target=len(logged) - under - over,
                days_under=under,
                days_over=over,
            )
        )
    per_day = {
        d.isoformat(): {
            key: round(value, 2) for key, value in totals_by_day[d.isoformat()].items()
        }
        for d in logged
    }
    stats = WeekStats(days=days, logged_days=logged, categories=categories, per_day=per_day)
    return WeekStats(
        days=days,
        logged_days=logged,
        categories=categories,
        per_day=per_day,
        patterns=_find_patterns(stats),
    )


def _find_patterns(stats: WeekStats) -> list[str]:
    patterns: list[str] = []
    logged = len(stats.logged_days)
    if stats.unlogged_days:
        missing = ", ".join(fmt_day(d) for d in stats.unlogged_days)
        patterns.append(f"ימים בלי תיעוד: {missing}")
    if logged < MIN_DAYS_FOR_PATTERN:
        return patterns

    needed = math.ceil(CONSISTENT_SHARE * logged)
    scored = [c for c in stats.categories if c.adherence_pct is not None]
    for c in scored:
        target = c.category.daily_target or 0.0
        if c.days_under >= needed:
            patterns.append(
                f"{c.category.name}: מתחת ליעד ב-{c.days_under} מתוך {logged} ימים "
                f"(ממוצע {fmt_num(c.avg_per_logged_day)} מתוך {fmt_num(target)})"
            )
        elif c.days_over >= needed:
            patterns.append(
                f"{c.category.name}: מעל היעד ב-{c.days_over} מתוך {logged} ימים "
                f"(ממוצע {fmt_num(c.avg_per_logged_day)} מתוך {fmt_num(target)})"
            )
    if len(scored) >= 2:
        best = max(scored, key=lambda c: c.adherence_pct or 0.0)
        worst = min(scored, key=lambda c: c.adherence_pct or 0.0)
        if (best.adherence_pct or 0.0) - (worst.adherence_pct or 0.0) >= 10:
            patterns.append(
                f"הכי קרוב ליעד: {best.category.name} ({round(best.adherence_pct or 0.0)}%), "
                f"הכי רחוק: {worst.category.name} ({round(worst.adherence_pct or 0.0)}%)"
            )
    return patterns


def format_week(stats: WeekStats) -> str:
    first, last = stats.days[0], stats.days[-1]
    lines = [
        f"📅 סיכום שבועי · {first.day}.{first.month} עד {last.day}.{last.month}",
        f"ימים עם תיעוד: {len(stats.logged_days)} מתוך {len(stats.days)}",
    ]
    overall = stats.overall_adherence_pct
    if overall is not None:
        lines.append(f"עמידה כוללת ביעדים: {round(overall)}%")
    lines.append("")
    for c in stats.categories:
        category = c.category
        if c.adherence_pct is None:
            lines.append(
                f"{category.name}: ממוצע {fmt_qty(c.avg_per_logged_day, category)} ליום"
            )
            continue
        target = category.daily_target or 0.0
        lines.append(
            f"{category.name}: ממוצע {fmt_num(c.avg_per_logged_day)} מתוך "
            f"{fmt_qty(target, category)} ליום · עמידה ביעד {round(c.adherence_pct)}%"
        )
        lines.append(
            f"   ביעד {c.days_on_target} ימים · מתחת {c.days_under} · מעל {c.days_over}"
        )
    return "\n".join(lines)


def format_patterns(stats: WeekStats) -> str:
    """Deterministic fallback for when Claude can't phrase the insights."""
    if not stats.patterns:
        return ""
    return "מה בלט השבוע:\n" + "\n".join(f"• {p}" for p in stats.patterns)


def week_payload(stats: WeekStats) -> dict[str, Any]:
    """The week's numbers in a JSON-ready shape, for Claude to phrase."""
    return {
        "first_day": stats.days[0].isoformat(),
        "last_day": stats.days[-1].isoformat(),
        "days_in_range": len(stats.days),
        "logged_days": [d.isoformat() for d in stats.logged_days],
        "days_without_any_log": [d.isoformat() for d in stats.unlogged_days],
        "overall_adherence_pct": stats.overall_adherence_pct,
        "categories": [
            {
                "key": c.category.key,
                "name": c.category.name,
                "daily_target": c.category.daily_target,
                "avg_per_logged_day": c.avg_per_logged_day,
                "adherence_pct": c.adherence_pct,
                "days_on_target": c.days_on_target,
                "days_under_target": c.days_under,
                "days_over_target": c.days_over,
            }
            for c in stats.categories
        ],
        "portions_per_day": stats.per_day,
        "detected_patterns": stats.patterns,
    }
